require_relative 'vagrant/lib/vagrant/libreelec/plugin'

# All boxes used below support libvirt; the LibreELEC and OSMC boxes *only*
# support libvirt.
ENV['VAGRANT_DEFAULT_PROVIDER'] = 'libvirt'

# If possible, tie on-demand box building to box addition events; otherwise,
# perform builds on `vagrant up`.
def trigger_box_build(config, &block)
  if Vagrant.version?(">= 2.4.0") || ENV.fetch('VAGRANT_EXPERIMENTAL', '').split(/,/).include?('typed_triggers')
    config.trigger.before(%I[Vagrant::Action::Builtin::BoxAdd Vagrant::Action::Building::HandleBox], type: :action, &block)
  else
    config.trigger.before(:up, &block)
  end
end

# Provision with the test playbook.  This is called from each machine
# definition, rather than defined once at the top level, so that
# machine-specific provisioners (e.g. the one that installs `sudo` on Alpine)
# run first; Vagrant runs top-level provisioners before machine-level ones.
def provision_with_ansible(machine)
  machine.vm.provision :ansible, type: 'ansible' do |ansible|
    ansible.playbook = 'tests/test.yml'
    ansible.verbose = 'vv'

    # Vagrant uses the `--sudo` flag when `ansible.become` is enabled;
    # evidently Ansible no longer supports `--sudo`.
    #ansible.become = true
    ansible.raw_arguments = %w[--become]
  end
end

Vagrant.configure(2) do |config|
  # We don't need to sync this folder onto the guest, and disabling the synced
  # folder prevents Vagrant from bailing out on guests that lack `rsync` (e.g.
  # LibreELEC).
  config.vm.synced_folder '.', '/vagrant', disabled: true

  config.vm.provider :libvirt do |domain|
    # Listen on all addresses
    domain.graphics_ip = '::'
  end

  # Alpine Linux, current stable branches:
  # https://alpinelinux.org/releases/
  %w[3.22 3.23 3.24].each do |version|
    config.vm.define :"alpine#{version.delete('.')}" do |alpine|
      alpine.vm.box = "cloud-image/alpine-#{version}"

      # The Alpine cloud image ships neither `bash` nor `sudo`; it uses `doas`
      # instead.  Install `sudo` so that Ansible's default `become` method
      # works.
      alpine.ssh.shell = 'sh'
      alpine.ssh.sudo_command = 'doas %c'
      alpine.vm.provision :install_sudo, type: :shell, privileged: true, inline: <<~SHELL
        #!/bin/sh
        set -eu
        apk add --no-cache sudo
        echo 'vagrant ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/vagrant
        chmod 0440 /etc/sudoers.d/vagrant
      SHELL

      provision_with_ansible(alpine)
    end
  end

  # The official `archlinux/archlinux` box is no longer published (all versions
  # are revoked), so use a box built from the official Arch Linux cloud image.
  config.vm.define :archlinux do |archlinux|
    archlinux.vm.box = 'cloud-image/arch-linux'
    provision_with_ansible(archlinux)
  end

  # Debian oldstable (LTS) and stable:
  # https://www.debian.org/releases/
  config.vm.define :debian12 do |debian12|
    debian12.vm.box = 'debian/bookworm64'
    provision_with_ansible(debian12)
  end

  config.vm.define :debian13 do |debian13|
    debian13.vm.box = 'debian/trixie64'
    provision_with_ansible(debian13)
  end

  # Ubuntu LTS releases still in standard support:
  # https://ubuntu.com/about/release-cycle
  #
  # There is no libvirt box for the current interim release (26.10); it is
  # covered by the GitHub Actions workflow instead.
  %w[22.04 24.04 26.04].each do |version|
    config.vm.define :"ubuntu#{version.delete('.')}" do |ubuntu|
      ubuntu.vm.box = "cloud-image/ubuntu-#{version}"
      provision_with_ansible(ubuntu)
    end
  end

  config.vm.define :libreelec do |libreelec|
    box = File.expand_path('tmp/libreelec/LibreELEC-Generic.x86_64-12.2.1.box', __dir__)

    trigger_box_build(libreelec) do |t|
      t.name = 'Create LibreELEC box'
      t.info = "Ensuring that the LibreELEC box at #{box.inspect} exists"
      unless File.exist?(box)
        t.run = {
          path: File.expand_path('scripts/create-libreelec-box', __dir__),
          args: [box],
        }
      end
    end

    libreelec.vm.box_url = "file://#{box}"
    libreelec.vm.box = 'libreelec'
    provision_with_ansible(libreelec)
  end

  config.vm.define :osmc do |osmc|
    box = File.expand_path('tmp/osmc/OSMC_TGT_rbp4_20260920.box', __dir__)

    trigger_box_build(osmc) do |t|
      t.name = 'Create OSMC box'
      t.info = "Ensuring that the OSMC box at #{box.inspect} exists"
      t.warn = <<~WARN.gsub(/(?<!^)\n/, ' ')
        The `osmc` box's Vagrantfile defines a Vagrant trigger that installs a
        custom kernel and initial ramdisk to a location accessible to the libvirt daemon.

        This trigger *must* run in order to boot machines using the `osmc` box.

        A bug in Vagrant prevents triggers defined in a box's Vagrantfile from
        running when the box is first added; see https://github.com/hashicorp/vagrant/issues/11901.

        If your OSMC virtual machine hangs upon boot, or if Vagrant crashes
        bringing up the OSMC virtual machine, please try running `vagrant up` a
        second time, as Vagrant will then properly execute the `up` trigger
        mentioned above.
      WARN
      unless File.exist?(box)
        t.run = {
          path: File.expand_path('scripts/create-osmc-box', __dir__),
          args: [box],
        }
      end
    end

    osmc.vm.box_url = "file://#{box}"
    osmc.vm.box = 'osmc'
    provision_with_ansible(osmc)
  end
end
